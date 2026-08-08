import os
from collections.abc import Callable
from pathlib import Path

from oms_hub.files.atomic import sha256_file, verified_atomic_copy


class PromotionRecoveryError(RuntimeError):
    pass


class PromotionSourceError(RuntimeError):
    pass


class PromotionCoordinator:
    def promote[T](
        self,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
        commit: Callable[[], T],
    ) -> T:
        _validate_sources(pairs)
        try:
            return promote_with_rollback(pairs, revision_id, commit)
        except OSError as error:
            raise PromotionRecoveryError(
                "slide file promotion could not complete"
            ) from error

    def recover[T](
        self,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
        commit: Callable[[], T],
        reset: Callable[[], None],
    ) -> T | None:
        source_hashes = _validate_sources(pairs)
        try:
            return recover_promotion(
                pairs,
                revision_id,
                commit,
                reset,
                source_hashes,
            )
        except OSError as error:
            raise PromotionRecoveryError(
                "slide file promotion recovery could not complete"
            ) from error

    def remove_backups(
        self,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> None:
        remove_backups(pairs, revision_id)

    def restore_backups(
        self,
        pairs: list[tuple[Path, Path]],
        revision_id: int,
    ) -> None:
        try:
            _roll_back_recovery(pairs, revision_id)
        except OSError as error:
            raise PromotionRecoveryError(
                "slide file promotion recovery could not complete"
            ) from error

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
        result = commit()
    except Exception as error:
        rollback_error: OSError | None = None
        for destination, saved in backups.items():
            try:
                if saved is not None and saved.exists():
                    os.replace(saved, destination)
                elif saved is None:
                    destination.unlink(missing_ok=True)
            except OSError as restore_error:
                rollback_error = rollback_error or restore_error
        if rollback_error is not None:
            raise rollback_error from error
        raise
    remove_backups(pairs, revision_id)
    return result


def recover_promotion[T](
    pairs: list[tuple[Path, Path]],
    revision_id: int,
    commit: Callable[[], T],
    reset: Callable[[], None],
    source_hashes: dict[Path, str] | None = None,
) -> T | None:
    expected = source_hashes or _validate_sources(pairs)
    if all(
        destination.is_file()
        and sha256_file(destination) == expected[source]
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
    for _source, destination in pairs:
        saved = backup_path(destination, revision_id)
        if saved.exists():
            os.replace(saved, destination)


def _validate_sources(pairs: list[tuple[Path, Path]]) -> dict[Path, str]:
    hashes: dict[Path, str] = {}
    try:
        for source, _destination in pairs:
            if not source.is_file():
                raise FileNotFoundError(source)
            hashes[source] = sha256_file(source)
    except OSError as error:
        raise PromotionSourceError(
            "immutable promotion source is unavailable; upload the file again"
        ) from error
    return hashes


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
