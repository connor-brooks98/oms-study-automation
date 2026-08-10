import pytest

import oms_hub.files.promotion as promotion_module
from oms_hub.files.atomic import verified_atomic_copy
from oms_hub.files.promotion import PromotionCoordinator, PromotionRecoveryError


class _AlwaysOwned:
    def assert_owned(self) -> None:
        return None


CLAIM = _AlwaysOwned()


def test_promotion_restores_prior_file_when_database_commit_fails(tmp_path):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    coordinator = PromotionCoordinator()

    def fail_commit():
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        coordinator.promote([(source, destination)], 12, fail_commit, CLAIM)

    assert destination.read_bytes() == b"old"
    assert not coordinator.backup_path(destination, 12).exists()


def test_recovery_commits_completed_file_promotion_and_removes_backups(tmp_path):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"new")
    coordinator = PromotionCoordinator()
    backup = coordinator.backup_path(destination, 20)
    backup.write_bytes(b"old")
    calls: list[str] = []

    result = coordinator.recover(
        [(source, destination)],
        20,
        lambda: calls.append("commit") or "committed",
        lambda: calls.append("reset"),
        CLAIM,
    )

    assert result == "committed"
    assert calls == ["commit"]
    assert destination.read_bytes() == b"new"
    assert not backup.exists()


def test_recovery_restores_prior_file_when_database_commit_still_fails(tmp_path):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"new")
    coordinator = PromotionCoordinator()
    backup = coordinator.backup_path(destination, 25)
    backup.write_bytes(b"old")
    calls: list[str] = []

    def fail_commit():
        calls.append("commit")
        raise RuntimeError("database unavailable")

    with pytest.raises(RuntimeError, match="database unavailable"):
        coordinator.recover(
            [(source, destination)],
            25,
            fail_commit,
            lambda: calls.append("reset"),
            CLAIM,
        )

    assert calls == ["commit", "reset"]
    assert destination.read_bytes() == b"old"
    assert not backup.exists()


def test_recovery_preserves_ambiguous_matching_destination_without_backup(tmp_path):
    first_source = tmp_path / "first-immutable.pdf"
    second_source = tmp_path / "second-immutable.pdf"
    first_destination = tmp_path / "first-current.pdf"
    second_destination = tmp_path / "second-current.pdf"
    first_source.write_bytes(b"first")
    second_source.write_bytes(b"second")
    verified_atomic_copy(first_source, first_destination)
    calls: list[str] = []

    result = PromotionCoordinator().recover(
        [
            (first_source, first_destination),
            (second_source, second_destination),
        ],
        30,
        lambda: calls.append("commit"),
        lambda: calls.append("reset"),
        CLAIM,
    )

    assert result is None
    assert calls == ["reset"]
    assert first_destination.read_bytes() == b"first"
    assert not second_destination.exists()


def test_recovery_translates_locked_destination_to_retryable_error(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"partial")
    coordinator = PromotionCoordinator()
    coordinator.backup_path(destination, 31).write_bytes(b"old")

    def locked_replace(_source, _destination):
        raise OSError("destination is locked")

    monkeypatch.setattr(promotion_module.os, "replace", locked_replace)

    with pytest.raises(PromotionRecoveryError) as error:
        coordinator.recover(
            [(source, destination)],
            31,
            lambda: None,
            lambda: None,
            CLAIM,
        )

    assert isinstance(error.value.__cause__, OSError)
    assert "destination is locked" in str(error.value.__cause__)
