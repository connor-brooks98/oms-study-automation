from __future__ import annotations

from pathlib import Path


def is_indirection(path: Path) -> bool:
    """Fail closed for symlinks, Windows junctions, and inspection failures."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction()) if callable(is_junction) else False
    except OSError:
        return True


def trusted_existing_directory(path: Path) -> bool:
    """Require a real directory and real directory ancestors through ``/``."""
    try:
        if not path.is_absolute():
            return False
        current = path
        while True:
            if is_indirection(current) or not current.is_dir():
                return False
            parent = current.parent
            if parent == current:
                return True
            current = parent
    except OSError:
        return False


def prepare_trusted_directory(path: Path) -> bool:
    """Create a missing directory only below an already trusted real parent."""
    try:
        if not path.is_absolute():
            return False
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                return False
            current = parent
        if not trusted_existing_directory(current):
            return False
        for directory in reversed(missing):
            if not trusted_existing_directory(directory.parent):
                return False
            directory.mkdir()
            if not trusted_existing_directory(directory):
                return False
        return trusted_existing_directory(path)
    except OSError:
        return False


def trusted_managed_path(
    path: Path,
    root: Path,
    *,
    require_regular_file: bool,
) -> bool:
    """Validate a path component-by-component against a trusted root.

    Lexical containment is checked before resolving so an outside spelling that
    resolves inward is never accepted.  Every component from the configured
    root to the leaf is inspected for symlink/junction indirection.
    """
    try:
        if not trusted_existing_directory(root):
            return False
        if not path.is_absolute() or not path.is_relative_to(root):
            return False
        relative = path.relative_to(root)
        if any(part in {".", ".."} for part in relative.parts):
            return False
        current = root
        for part in relative.parts:
            current = current / part
            if is_indirection(current):
                return False
        if not path.resolve().is_relative_to(root.resolve()):
            return False
        if require_regular_file:
            return path.is_file()
        return not path.exists() or path.is_file()
    except OSError:
        return False
