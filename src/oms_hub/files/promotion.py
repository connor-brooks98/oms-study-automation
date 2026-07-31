import os
from collections.abc import Callable
from pathlib import Path

from oms_hub.files.atomic import sha256_file, verified_atomic_copy


class PromotionCoordinator:
    def promote[T](
        self,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
        commit: Callable[[], T],
    ) -> T:
        return promote_with_rollback(pairs, revision_id, commit)

    def recover[T](
        self,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
        commit: Callable[[], T],
        reset: Callable[[], None],
    ) -> T | None:
        return recover_promotion(pairs, revision_id, commit, reset)

    def remove_backups(
        self,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> None:
        remove_backups(pairs, revision_id)

    def backup_path(self, destination: Path, revision_id: int) -> Path:
        return backup_path(destination, revision_id)


def promote_with_rollback[T](
    pairs: list[tuple[Path, Path]],
    revision_id: int,
    commit: Callable[[], T],
) -> T:
    backups: dict[Path, Path | None] = {}
    try:
        for _, destination in pairs:
            if destination.exists():
                backup = backup_path(destination, revision_id)
                verified_atomic_copy(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None
        for source, destination in pairs:
            verified_atomic_copy(source, destination)
        return commit()
    except Exception:
        for destination, saved in backups.items():
            if saved is not None and saved.exists():
                os.replace(saved, destination)
            elif saved is None:
                destination.unlink(missing_ok=True)
        raise
    finally:
        for saved in backups.values():
            if saved is not None:
                saved.unlink(missing_ok=True)


def recover_promotion[T](
    pairs: list[tuple[Path, Path]],
    revision_id: int,
    commit: Callable[[], T],
    reset: Callable[[], None],
) -> T | None:
    if all(
        destination.is_file()
        and sha256_file(destination) == sha256_file(source)
        for source, destination in pairs
    ):
        try:
            result = commit()
        except Exception:
            _roll_back_recovery(pairs, revision_id)
            reset()
            raise
        remove_backups(pairs, revision_id)
        return result
    _roll_back_recovery(pairs, revision_id)
    reset()
    return None


def _roll_back_recovery(
    pairs: list[tuple[Path, Path]],
    revision_id: int,
) -> None:
    for source, destination in pairs:
        saved = backup_path(destination, revision_id)
        if saved.exists():
            os.replace(saved, destination)
        elif (
            destination.is_file()
            and sha256_file(destination) == sha256_file(source)
        ):
            destination.unlink()


def remove_backups(
    pairs: list[tuple[Path, Path]],
    revision_id: int,
) -> None:
    for _, destination in pairs:
        backup_path(destination, revision_id).unlink(missing_ok=True)


def backup_path(destination: Path, revision_id: int) -> Path:
    return destination.with_name(
        f".{destination.name}.oms-backup-{revision_id}"
    )
