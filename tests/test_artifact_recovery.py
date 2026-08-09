import pytest

import oms_hub.artifacts as artifacts_module
from oms_hub.artifacts import ArtifactRecoveryError, ArtifactService


def test_rollback_restore_failure_keeps_verified_backup_and_journal(tmp_path, monkeypatch):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    backup = ArtifactService._backup_path(destination, 42)
    original_copy = artifacts_module.verified_atomic_copy

    def fail_restore(copy_source, copy_destination):
        if copy_source == backup and copy_destination == destination:
            raise OSError("destination is locked during rollback")
        return original_copy(copy_source, copy_destination)

    monkeypatch.setattr(artifacts_module, "verified_atomic_copy", fail_restore)

    def fail_commit():
        raise RuntimeError("database commit failed")

    with pytest.raises(ArtifactRecoveryError) as raised:
        ArtifactService._promote_with_rollback(
            [(source, destination)],
            42,
            fail_commit,
        )

    error = raised.value
    assert isinstance(error.__cause__, RuntimeError)
    assert isinstance(error.restore_error, OSError)
    assert error.backup_paths == (backup,)
    assert backup.read_bytes() == b"old"
    assert error.recovery_journal_path.is_file()
    assert "current.pdf" in error.recovery_journal_path.read_text(encoding="utf-8")


def test_successful_promotion_removes_verified_backup_after_commit(tmp_path):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    ArtifactService._promote_with_rollback(
        [(source, destination)],
        43,
        lambda: None,
    )

    assert destination.read_bytes() == b"new"
    assert not ArtifactService._backup_path(destination, 43).exists()


def test_partial_backup_failure_does_not_mask_original_error_with_journal_keyerror(
    tmp_path,
    monkeypatch,
):
    first_source = tmp_path / "first-immutable.pdf"
    first_destination = tmp_path / "first-current.pdf"
    second_source = tmp_path / "second-immutable.pdf"
    second_destination = tmp_path / "second-current.pdf"
    first_source.write_bytes(b"first-new")
    first_destination.write_bytes(b"first-old")
    second_source.write_bytes(b"second-new")
    second_destination.write_bytes(b"second-old")
    original_copy = artifacts_module.verified_atomic_copy

    def fail_second_backup(copy_source, copy_destination):
        if copy_source == second_destination:
            raise OSError("second destination backup is locked")
        return original_copy(copy_source, copy_destination)

    monkeypatch.setattr(artifacts_module, "verified_atomic_copy", fail_second_backup)

    with pytest.raises(OSError, match="second destination backup is locked"):
        ArtifactService._promote_with_rollback(
            [
                (first_source, first_destination),
                (second_source, second_destination),
            ],
            44,
            lambda: None,
        )

    assert first_destination.read_bytes() == b"first-old"
    assert second_destination.read_bytes() == b"second-old"


def test_journal_failure_still_restores_destination_and_keeps_original_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    def fail_journal(*_args, **_kwargs):
        raise OSError("recovery journal disk is unavailable")

    monkeypatch.setattr(
        ArtifactService,
        "_write_recovery_journal",
        staticmethod(fail_journal),
    )

    def fail_commit():
        raise RuntimeError("database commit failed")

    with pytest.raises(RuntimeError, match="database commit failed") as raised:
        ArtifactService._promote_with_rollback(
            [(source, destination)],
            45,
            fail_commit,
        )

    assert isinstance(raised.value.__cause__, OSError)
    assert destination.read_bytes() == b"old"
    assert not ArtifactService._backup_path(destination, 45).exists()


def test_dual_journal_and_restore_failure_keeps_backup_without_journal_path(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "immutable.pdf"
    destination = tmp_path / "current.pdf"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    backup = ArtifactService._backup_path(destination, 46)
    original_copy = artifacts_module.verified_atomic_copy

    def fail_restore(copy_source, copy_destination):
        if copy_source == backup and copy_destination == destination:
            raise OSError("rollback destination remains locked")
        return original_copy(copy_source, copy_destination)

    def fail_journal(*_args, **_kwargs):
        raise OSError("recovery journal disk is unavailable")

    monkeypatch.setattr(artifacts_module, "verified_atomic_copy", fail_restore)
    monkeypatch.setattr(
        ArtifactService,
        "_write_recovery_journal",
        staticmethod(fail_journal),
    )

    with pytest.raises(ArtifactRecoveryError) as raised:
        ArtifactService._promote_with_rollback(
            [(source, destination)],
            46,
            lambda: (_ for _ in ()).throw(RuntimeError("database commit failed")),
        )

    error = raised.value
    assert error.recovery_journal_path is None
    assert error.backup_paths == (backup,)
    assert isinstance(error.original_error, RuntimeError)
    assert isinstance(error.restore_error, OSError)
    assert isinstance(error.journal_error, OSError)
    assert backup.read_bytes() == b"old"
